"""Optional local commentary via Ollama.

Strictly narrative. The model receives a finished analysis and writes two to four
sentences of plain language about it. It never produces, adjusts, or second-guesses a
price, level, score, or verdict — the analysis is deterministic and does not need a
model's opinion.

Two defences enforce that, because a local model hallucinating a stop-loss into a
trading dashboard is the worst thing this project could do:

1. The prompt carries a curated fact sheet and instructs the model to write no
   numerals at all.
2. `validate_numbers()` re-reads the output and rejects it if it contains any number
   that is not present in the input. Rejected commentary is dropped and the reason is
   surfaced, never silently swallowed.

Ollama is never a hard dependency. If it is missing, commentary is disabled and every
other part of the dashboard works normally.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("llm")

# Below this, Persian output quality falls off badly for this task.
MIN_COMFORTABLE_PARAMS_B = 7.0
MIN_USABLE_PARAMS_B = 3.0
# Persian commentary is held to a higher bar than English, from measurement rather
# than theory: qwen2.5:3b produced Farsi that was not just clumsy but semantically
# wrong about the trade — calling gates "شاپ‌ها/فروشگاه‌ها" and inventing phrases like
# "ترک بانک Bitcoin". The number guard still held, but prose that misdescribes the
# analysis is its own kind of false confidence. English from the same model was fine.
# Override with settings.llm.allow_weak_persian if you want it anyway.
MIN_PARAMS_FOR_PERSIAN_B = 7.0
# Rough resident footprint of a Q4 model: ~0.6 GB per billion params, plus overhead.
GB_PER_B_Q4 = 0.6
RUNTIME_OVERHEAD_GB = 1.0
# Headroom left for macOS and everything else the user has open.
OS_RESERVE_GB = 3.0


# --------------------------------------------------------------------------------
# Hardware
# --------------------------------------------------------------------------------


def hardware() -> dict:
    system = platform.system()
    machine = platform.machine()
    info = {
        "platform": f"{system} {platform.release()}",
        "arch": machine,
        "cpu_cores": os.cpu_count(),
        "cpu_model": None,
        "total_ram_gb": None,
        "gpu": None,
        "vram_mb": None,
        "unified_memory": False,
        "accelerated": False,
    }

    if system == "Darwin":
        info["cpu_model"] = _sysctl("machdep.cpu.brand_string")
        mem = _sysctl("hw.memsize")
        if mem:
            info["total_ram_gb"] = round(int(mem) / 1024**3, 1)
        if machine == "arm64":
            # Apple silicon shares one pool between CPU and GPU, and Ollama uses Metal.
            info["gpu"] = "Apple silicon (Metal)"
            info["unified_memory"] = True
            info["accelerated"] = True
            info["vram_mb"] = int((info["total_ram_gb"] or 0) * 1024)
        else:
            chipset, vram = _mac_gpu()
            info["gpu"] = chipset
            info["vram_mb"] = vram
            # Ollama has no Metal path on Intel Macs; inference falls back to CPU.
            info["accelerated"] = False
    else:
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        info["total_ram_gb"] = round(int(line.split()[1]) / 1024**2, 1)
                        break
        except OSError:
            pass
        nv = shutil.which("nvidia-smi")
        if nv:
            out = _cmd([nv, "--query-gpu=name,memory.total",
                        "--format=csv,noheader,nounits"])
            if out:
                first = out.splitlines()[0].split(",")
                info["gpu"] = first[0].strip()
                if len(first) > 1 and first[1].strip().isdigit():
                    info["vram_mb"] = int(first[1].strip())
                info["accelerated"] = True
    return info


def _sysctl(key: str) -> str | None:
    return _cmd(["sysctl", "-n", key])


def _mac_gpu() -> tuple[str | None, int | None]:
    out = _cmd(["system_profiler", "SPDisplaysDataType"])
    if not out:
        return None, None
    chipset = vram = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Chipset Model:") and not chipset:
            chipset = line.split(":", 1)[1].strip()
        elif "VRAM" in line and vram is None:
            m = re.search(r"(\d+)\s*MB", line)
            if m:
                vram = int(m.group(1))
    return chipset, vram


def _cmd(args: list[str], timeout: int = 10) -> str | None:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------------


def host() -> str:
    return (os.environ.get("OLLAMA_HOST")
            or config.load_settings()["llm"].get("host")
            or "http://127.0.0.1:11434").rstrip("/")


def _api(path: str, payload: dict | None = None, timeout: int = 10):
    url = f"{host()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status() -> dict:
    """Is Ollama installed, and is it answering?"""
    out = {"installed": bool(shutil.which("ollama")), "running": False,
           "host": host(), "error": None, "models": []}
    try:
        tags = _api("/api/tags", timeout=6)
        out["running"] = True
        out["models"] = _parse_models(tags)
    except urllib.error.URLError as exc:
        out["error"] = f"cannot reach Ollama at {out['host']}: {exc.reason}"
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _parse_models(tags: dict) -> list[dict]:
    models = []
    for m in (tags or {}).get("models", []):
        d = m.get("details") or {}
        models.append({
            "name": m.get("name"),
            "size_gb": round((m.get("size") or 0) / 1024**3, 2),
            "parameter_size": d.get("parameter_size"),
            "parameters_b": _params_to_billions(d.get("parameter_size")),
            "quantization": d.get("quantization_level"),
            "context_length": d.get("context_length"),
            "family": d.get("family"),
        })
    return models


def _params_to_billions(text: str | None) -> float | None:
    if not text:
        return None
    m = re.match(r"([\d.]+)\s*([BM])", str(text).strip(), re.I)
    if not m:
        return None
    value = float(m.group(1))
    return value / 1000 if m.group(2).upper() == "M" else value


# --------------------------------------------------------------------------------
# Model selection
# --------------------------------------------------------------------------------


def _fits(params_b: float, hw: dict) -> bool:
    ram = hw.get("total_ram_gb")
    if not ram:
        return True
    return params_b * GB_PER_B_Q4 + RUNTIME_OVERHEAD_GB <= ram - OS_RESERVE_GB


def recommend_for(hw: dict) -> dict:
    """Pick a model to suggest pulling, sized to this machine."""
    ram = hw.get("total_ram_gb") or 8
    budget = ram - OS_RESERVE_GB
    candidates = [
        ("qwen2.5:14b-instruct-q4_K_M", 14.0,
         "strongest Persian of the three; needs real headroom"),
        ("qwen2.5:7b-instruct-q4_K_M", 7.6,
         "the sweet spot for this job — solid instruction-following and good Persian"),
        ("qwen2.5:3b-instruct-q4_K_M", 3.1,
         "runs almost anywhere, but Persian is noticeably weaker"),
    ]
    for name, params_b, why in candidates:
        need = params_b * GB_PER_B_Q4 + RUNTIME_OVERHEAD_GB
        if need <= budget:
            return {"model": name, "parameters_b": params_b, "why": why,
                    "estimated_ram_gb": round(need, 1),
                    "command": f"ollama pull {name}"}
    name, params_b, why = candidates[-1]
    return {"model": name, "parameters_b": params_b, "why": why,
            "estimated_ram_gb": round(params_b * GB_PER_B_Q4 + RUNTIME_OVERHEAD_GB, 1),
            "command": f"ollama pull {name}"}


def persian_upgrade(hw: dict) -> dict:
    """The smallest model that clears the Persian bar, with an honest RAM caveat.

    Kept separate from `recommend_for`, which only suggests what fits comfortably —
    on a machine too small for 7B that would recommend the 3B already installed and
    say nothing useful about fixing Persian.
    """
    name, params_b = "qwen2.5:7b-instruct-q4_K_M", 7.6
    need = params_b * GB_PER_B_Q4 + RUNTIME_OVERHEAD_GB
    ram = hw.get("total_ram_gb")
    budget = (ram - OS_RESERVE_GB) if ram else None
    fits = budget is None or need <= budget
    return {
        "model": name, "parameters_b": params_b,
        "estimated_ram_gb": round(need, 1),
        "command": f"ollama pull {name}",
        "fits_comfortably": fits,
        "caveat": None if fits else (
            f"needs about {need:.1f} GB against roughly {budget:.0f} GB usable here, "
            f"so it will lean on swap and run slowly — but it is the smallest model "
            f"that produces trustworthy Persian for this task"),
    }


def assess(st: dict | None = None, hw: dict | None = None) -> dict:
    """Decide whether any installed model is suitable, and say why.

    The job is short bilingual summarisation of structured JSON: it needs solid
    instruction-following and decent Persian, not reasoning horsepower.
    """
    st = st or status()
    hw = hw or hardware()
    reasoning: list[str] = []

    if not st["installed"]:
        reasoning.append("Ollama is not installed — commentary disabled. "
                         "Everything else works; the analysis is deterministic.")
        return _decision(None, False, reasoning, hw, recommend_for(hw))
    if not st["running"]:
        reasoning.append(f"Ollama is installed but not answering ({st['error']}). "
                         "Start it with `ollama serve`. Commentary disabled meanwhile.")
        return _decision(None, False, reasoning, hw, recommend_for(hw))

    ram = hw.get("total_ram_gb")
    if hw.get("accelerated"):
        reasoning.append(f"GPU acceleration available ({hw.get('gpu')}).")
    else:
        reasoning.append(
            f"No GPU acceleration ({hw.get('gpu') or 'no GPU detected'}), so inference "
            f"runs on {hw.get('cpu_cores')} CPU cores and will be slow. Commentary is "
            f"generated on demand rather than for every coin in a scan.")
    if ram:
        reasoning.append(
            f"{ram} GB RAM total, about {max(0, ram - OS_RESERVE_GB):.0f} GB usable for "
            f"a model after leaving room for the OS.")

    if not st["models"]:
        reasoning.append("No models installed.")
        return _decision(None, False, reasoning, hw, recommend_for(hw))

    usable = []
    for m in st["models"]:
        p = m.get("parameters_b")
        if p is None:
            reasoning.append(f"{m['name']}: parameter count unknown, skipped.")
            continue
        if not _fits(p, hw):
            reasoning.append(
                f"{m['name']} ({m['parameter_size']}) needs roughly "
                f"{p * GB_PER_B_Q4 + RUNTIME_OVERHEAD_GB:.1f} GB — too large for this "
                f"machine.")
            continue
        if p < MIN_USABLE_PARAMS_B:
            reasoning.append(
                f"{m['name']} ({m['parameter_size']}) is below ~3B; Persian output at "
                f"that size is unreliable for this task.")
            continue
        usable.append(m)

    if not usable:
        return _decision(None, False, reasoning, hw, recommend_for(hw))

    best = max(usable, key=lambda m: m["parameters_b"])
    comfortable = best["parameters_b"] >= MIN_COMFORTABLE_PARAMS_B
    if comfortable:
        reasoning.append(
            f"Selected {best['name']} ({best['parameter_size']}, "
            f"{best['quantization']}): comfortably above the ~7B mark this job wants.")
    else:
        rec = recommend_for(hw)
        reasoning.append(
            f"Selected {best['name']} ({best['parameter_size']}, "
            f"{best['quantization']}). This is below the ~7B this job really wants, so "
            f"expect serviceable English and weaker Persian. It is the best of what is "
            f"installed, not a model I would choose for Persian commentary.")
        if rec["model"] != best["name"]:
            reasoning.append(f"Better option if you want it: `{rec['command']}` "
                             f"(~{rec['estimated_ram_gb']} GB) — {rec['why']}.")
    persian_ok = best["parameters_b"] >= MIN_PARAMS_FOR_PERSIAN_B
    upgrade = persian_upgrade(hw)
    if not persian_ok:
        note = f" ({upgrade['caveat']})" if upgrade.get("caveat") else ""
        reasoning.append(
            f"Persian commentary is disabled on {best['name']}. Tested on this "
            f"machine it produced Farsi that misdescribed the analysis, not merely "
            f"clumsy phrasing. English from the same model is fine. For Persian, "
            f"`{upgrade['command']}`{note}. Set llm.allow_weak_persian to true in "
            f"config/settings.json to use it anyway.")
    return _decision(best["name"], True, reasoning, hw, recommend_for(hw),
                     comfortable=comfortable, parameters_b=best["parameters_b"],
                     persian_ok=persian_ok, persian_upgrade=upgrade)


def _decision(model, suitable, reasoning, hw, recommendation, comfortable=False,
              parameters_b=None, persian_ok=False, persian_upgrade=None) -> dict:
    return {
        "model": model,
        "suitable": suitable,
        "comfortable": comfortable,
        "parameters_b": parameters_b,
        "persian_ok": persian_ok,
        "persian_upgrade": persian_upgrade,
        "reasoning": reasoning,
        "hardware": hw,
        "recommendation": recommendation,
        "commentary_enabled": bool(model),
    }


def ensure_decision(force: bool = False) -> dict:
    """Assess once and cache the result in settings.json so it is not re-derived
    on every start. `force=True` re-runs it."""
    settings = config.load_settings()
    cached = (settings.get("llm") or {}).get("decision")
    if cached and not force:
        return cached
    decision = assess()
    config.save_settings({"llm": {"decision": decision,
                                  "model": decision["model"],
                                  "enabled": decision["commentary_enabled"]}})
    for line in decision["reasoning"]:
        log.info("llm: %s", line)
    return decision


# --------------------------------------------------------------------------------
# Number validation — the guard that matters
# --------------------------------------------------------------------------------

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _normalise_number(text: str) -> str:
    """'1,234.50' -> '1234.5'. Comparison is on value, not formatting."""
    text = text.replace(",", "")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _numbers_in(value, out: set[str]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        out.add(_normalise_number(f"{value:.10f}".rstrip("0").rstrip(".")))
        out.add(_normalise_number(f"{value:.2f}"))
        out.add(_normalise_number(f"{value:.1f}"))
        out.add(_normalise_number(str(int(value))) if abs(value) < 1e15 else "")
        out.discard("")
        return
    if isinstance(value, str):
        for m in _NUMBER_RE.finditer(value.translate(_PERSIAN_DIGITS)):
            out.add(_normalise_number(m.group()))
        return
    if isinstance(value, dict):
        for v in value.values():
            _numbers_in(v, out)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            _numbers_in(v, out)


def validate_numbers(text: str, payload) -> tuple[bool, str | None]:
    """Reject output containing any number absent from the input.

    Persian and Arabic-Indic digits are folded to ASCII first, so a Farsi answer
    cannot smuggle a fabricated figure past the check.
    """
    allowed: set[str] = set()
    _numbers_in(payload, allowed)
    found = _NUMBER_RE.findall(text.translate(_PERSIAN_DIGITS))
    for raw in found:
        if _normalise_number(raw) not in allowed:
            return False, (f"output contained the number '{raw}', which does not "
                           f"appear in the analysis it was given")
    return True, None


# --------------------------------------------------------------------------------
# Commentary
# --------------------------------------------------------------------------------

_SYSTEM = (
    "You are a careful analyst's assistant. You are given the finished output of a "
    "deterministic trading-analysis tool. Your only job is to restate it in plain "
    "language.\n"
    "Absolute rules:\n"
    "- Write 2 to 4 sentences. No lists, no headings, no preamble.\n"
    "- Do NOT write any numeral, price, level, score, percentage or ratio. Refer to "
    "them in words instead ('the stop sits just above the entry').\n"
    "- Do NOT offer an opinion on whether to trade, and do not contradict, adjust or "
    "second-guess the verdict you were given.\n"
    "- Do NOT invent any fact that is not in the input.\n"
    "- If unresolved manual checks are listed, say plainly that the verdict is not "
    "yet confirmed."
)

_LANG_INSTRUCTION = {
    "en": "Write in clear, plain English.",
    "fa": ("به فارسی روان و ساده بنویس. از اصطلاحات رایج بازار استفاده کن: "
           "حد ضرر، حد سود، نقطه ورود، اهرم. هیچ عددی ننویس."),
}


def build_facts(coin: str, plan: dict, unresolved: list[str]) -> dict:
    """The curated fact sheet the model is allowed to see.

    Deliberately small and mostly non-numeric: the less numeric material in the
    prompt, the less there is for the model to misquote.
    """
    qual = plan.get("qualification") or {}
    return {
        "coin": coin,
        "verdict": qual.get("verdict"),
        "action": qual.get("action"),
        "side": plan.get("side"),
        "profile": plan.get("profile_label"),
        "failed_gates": [g["gate"] for g in qual.get("gates", []) if not g["passed"]],
        "failed_gate_details": [g["detail"] for g in qual.get("gates", [])
                                if not g["passed"]],
        "blockers": plan.get("blockers") or [],
        "warnings": plan.get("warnings") or [],
        "unresolved_manual_checks": unresolved,
        "stop_source": (plan.get("levels") or {}).get("stop_source"),
    }


def commentary(coin: str, plan: dict, unresolved: list[str], lang: str = "en") -> dict:
    settings = config.load_settings()
    llm_cfg = settings.get("llm") or {}
    if not llm_cfg.get("enabled") or not llm_cfg.get("model"):
        return {"status": "unavailable", "text": None, "model": None,
                "reason": "commentary disabled or no model selected"}

    decision = llm_cfg.get("decision") or {}
    if (lang == "fa" and not decision.get("persian_ok")
            and not llm_cfg.get("allow_weak_persian")):
        up = decision.get("persian_upgrade") or {}
        caveat = f" ({up['caveat']})" if up.get("caveat") else ""
        return {"status": "unsuitable_language", "text": None,
                "model": llm_cfg.get("model"),
                "reason": (f"{llm_cfg.get('model')} is below the "
                           f"~{MIN_PARAMS_FOR_PERSIAN_B:g}B needed for reliable Persian "
                           f"on this task; its Farsi misdescribed the analysis in "
                           f"testing. English commentary is available. For Persian: "
                           f"{up.get('command', 'ollama pull qwen2.5:7b-instruct-q4_K_M')}"
                           f"{caveat}")}

    facts = build_facts(coin, plan, unresolved)
    prompt = (f"{_LANG_INSTRUCTION.get(lang, _LANG_INSTRUCTION['en'])}\n\n"
              f"Analysis output:\n{json.dumps(facts, ensure_ascii=False, indent=2)}")
    model = llm_cfg["model"]
    # Persian needs noticeably more tokens per sentence than English; 220 truncated
    # Farsi output mid-sentence.
    budget = 480 if lang == "fa" else 260
    try:
        resp = _api("/api/chat", {
            "model": model,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": budget},
        }, timeout=int(llm_cfg.get("timeout_seconds", 120)))
    except Exception as exc:
        return {"status": "unavailable", "text": None, "model": model,
                "reason": f"Ollama call failed: {exc}"}

    text = ((resp or {}).get("message") or {}).get("content", "").strip()
    if not text:
        return {"status": "unavailable", "text": None, "model": model,
                "reason": "empty response"}

    ok, why = validate_numbers(text, facts)
    if not ok:
        log.warning("commentary for %s rejected: %s", coin, why)
        return {"status": "rejected", "text": None, "model": model, "reason": why}
    return {"status": "ok", "text": _trim_sentences(text, 4), "model": model,
            "reason": None}


_SENTENCE_END = re.compile(r"(?<=[.!?؟])\s+")


def _trim_sentences(text: str, limit: int) -> str:
    """Small models overrun the sentence budget. Cut at a sentence boundary rather
    than mid-clause, and only when there is genuinely more than asked for."""
    parts = [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]
    if len(parts) <= limit:
        return text.strip()
    return " ".join(parts[:limit])
