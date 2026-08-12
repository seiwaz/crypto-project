"""Local, read-only crypto screening agent.

Analysis is delegated entirely to the crypto-leverage-trade-plan skill. Nothing in
this package places, modifies, or cancels an order, and nothing here recomputes the
skill's risk maths.
"""

__all__ = ["config", "store", "skill"]
