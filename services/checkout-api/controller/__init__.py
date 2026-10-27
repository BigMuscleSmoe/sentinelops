"""The fault controller: breaks checkout-api on purpose, and clears it.

It writes the fault flag that checkout-api reads, and the deploy-history
records that explain the fault to whoever investigates. Like sim/, it holds
the answer to every incident, so later phases keep the agent's repo reads out
of here.
"""
