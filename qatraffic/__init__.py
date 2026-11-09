"""qa-traffic-bot: organic, long-running user traffic for a Cartesi qa-wallet-tester-app (sequencer variant) deployment.

Drives deterministic HD accounts through deposits, in-app transfers and withdrawals, follows
every withdrawal voucher until it can be executed, executes it, and checks the result against
an independent model of the application ledger.
"""

__version__ = "0.1.0"
