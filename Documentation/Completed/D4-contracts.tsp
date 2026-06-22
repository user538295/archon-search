namespace D4BatchEmit;

/** One batch of chunk records submitted to store.ingest_chunks() per ingest_file() call */
model ChunkBatch {
  /** 0-based index of this batch within the document */
  batchIndex: int32;

  /** Total batches = ceil(total_chunks / INGEST_CHUNK_BATCH_SIZE) */
  totalBatches: int32;

  /** Number of chunks in this batch; maximum = INGEST_CHUNK_BATCH_SIZE (512) */
  chunkCount: int32;
}

/** ingest_file() aggregates all per-batch ChunkIngestResults into this shape */
model BatchAggregation {
  /** Sum of chunks_ingested returned by each ingest_chunks() call */
  chunksCreated: int32;

  /** Logical OR of needs_recompute returned by each ingest_chunks() call */
  needsRecompute: boolean;

  /** Count of ingest_chunks() calls = ceil(total_chunks / INGEST_CHUNK_BATCH_SIZE) */
  batchesProcessed: int32;
}

interface BatchEmitContract {
  /** Pipeline aggregates N ChunkIngestResults (one per batch) into one IngestResult */
  aggregate(results: BatchAggregation): BatchAggregation;
}

/** C2: IncrementalCentroid always-on — ingest_chunks() always calls _do_update_meta_on_add().
 *  When _is_continuation=True, distinct_doc_count is forced to 0 so a single file
 *  batched into N calls increments doc_count by 1, not by N.
 */
model IncrementalCentroidUpdate {
  /** False for the first batch of a document; True for all subsequent batches of the same document */
  isContinuation: boolean;

  /** When isContinuation=True, the doc_count delta is forced to 0; chunk_count and centroid_sum still accumulate */
  docCountDelta: int32;
}

interface IncrementalCentroidContract {
  /** Called unconditionally by ingest_chunks() for every batch; centroid_incremental_enabled flag is removed */
  updateMeta(update: IncrementalCentroidUpdate): void;
}
