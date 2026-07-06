import "@typespec/http";
using Http;

@service(#{ title: "Search Status API (E0c additions)" })
namespace SearchStatusApi;

model SearchStatusDetail {
  max_fanout: int32;
  top_k_max: int32;
}

model StatusSearchFragment {
  search: SearchStatusDetail | null;
}

@route("/status")
interface Status {
  @get get(): StatusSearchFragment | UnauthorizedResponse;
}
