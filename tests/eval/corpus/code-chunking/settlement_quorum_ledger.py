"""Settlement quorum ledger reconciliation health check — heavy vocabulary
overlap with the refund query (settlement, quorum, ledger, reconciliation,
batch, chargeback) but is a periodic health-check job, not a refund
processing path."""


def check_settlement_quorum_ledger(node_id, quorum_votes, batch_id):
    """Check whether the settlement ledger quorum reconciles for a batch.

    Settlement quorum ledger reconciliation batch chargeback.
    """
    reconciliation_ok = len(quorum_votes) >= 2
    return {
        "node_id": node_id,
        "batch_id": batch_id,
        "reconciliation_ok": reconciliation_ok,
        "chargeback_alert": not reconciliation_ok,
    }
