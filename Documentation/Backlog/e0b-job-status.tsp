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
  /** L10: terminal state for FAILED jobs that age past retry_max_age_hours. */
  FAILED_EXPIRED,
}
