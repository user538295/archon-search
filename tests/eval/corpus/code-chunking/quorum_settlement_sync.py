"""Quorum-gated settlement batch sync — heavy vocabulary overlap with the
refund query (quorum, settlement, batch, ledger, reconciliation,
chargeback) but is about distributed consensus before a commit, not
refund processing."""


def sync_settlement_batch_commit(node_id, quorum_votes, settlement_ledger):
    """Commit a settlement batch to the ledger once quorum is reached.

    Quorum settlement batch ledger reconciliation chargeback.
    """
    if len(quorum_votes) < 2:
        return None
    reconciliation_total = sum(settlement_ledger.values())
    return {
        "node_id": node_id,
        "committed": True,
        "reconciliation_total": reconciliation_total,
        "chargeback_window_open": False,
    }
