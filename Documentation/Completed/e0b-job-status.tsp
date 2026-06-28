namespace JobStatusContract;

/** Internal seam: JobStatus additions for E0b L10. */
enum JobStatus {
  PENDING,
  QUEUED,
  RUNNING,
  DONE,
  FAILED,
  CANCELLED,
  CANCELLING,
  /** L10: terminal state for FAILED jobs that age past retry_max_age_hours OR exhaust retry_max_attempts. One-way transition from FAILED; job is abandoned and never retried. */
  FAILED_EXPIRED,
}
