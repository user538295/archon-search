namespace DocumentCursorContract;

model DocumentInfo {
  doc_id: string;
  source_path: string;
  chunk_count: int32;
  indexed_at: string;
}

model DocumentPage {
  items: DocumentInfo[];
  next_cursor: string | null;
  total: int32;
}

// Cursor semantics: cursor value is the doc_id of the last item on the previous page.
// Items are sorted by doc_id ascending (lexicographic). A missing or deleted cursor
// doc_id resumes from the first doc_id that sorts after the cursor value.
// If nothing sorts after the cursor value, items is empty and next_cursor is null.
// Absence of cursor (null/void) starts from the beginning of the sorted collection.
interface DocumentRepository {
  listDocuments(collection: string, limit: int32, cursor: string | void): DocumentPage;
}
