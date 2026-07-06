import "@typespec/http";
using Http;

@service(#{ title: "Collection Documents API" })
namespace DocumentsApi;

model DocumentInfo {
  doc_id: string;
  source_path: string;
  chunk_count: int32;
  indexed_at: string;
}

model DocumentListResponse {
  items: DocumentInfo[];
  next_cursor: string | null;
  total: int32;
}

@error
model ValidationError {
  @statusCode statusCode: 422;
  detail: string;
}

@route("/collections/{name}/documents")
interface Documents {
  @get list(
    @path name: string,
    @query @minValue(1) @maxValue(200) limit?: int32 = 50,
    @query cursor?: string,
  ): DocumentListResponse | NotFoundResponse | UnauthorizedResponse | ValidationError;
}
