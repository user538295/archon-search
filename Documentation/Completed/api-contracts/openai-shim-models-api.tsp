import "@typespec/http";
using Http;

@service(#{ title: "Archon OpenAI Shim — Models API" })
namespace OpenAIShimModelsApi;

model ModelObject {
  id: string;
  object: string;
  created: int32;
  owned_by: string;
}

model ModelList {
  object: string;
  data: ModelObject[];
}

model OpenAIError {
  message: string;
  type: string;
  param?: string | null;
  code?: string | null;
}

model OpenAIErrorResponse {
  error: OpenAIError;
}

@route("/v1")
interface Models {
  @get
  @route("/models")
  list(): ModelList | OpenAIErrorResponse;
}
