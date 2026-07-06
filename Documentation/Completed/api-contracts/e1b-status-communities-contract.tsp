import "@typespec/http";
using Http;

@service(#{ title: "E1B Status Communities API" })
namespace E1bStatusCommunities;

model StatusCollectionEntry {
  name: string;
  docCount: int32;
  chunkCount: int32;
  status: string;
  watching: boolean;
  /** Count of built Leiden communities. 0 if build-communities has never run. */
  communityCount: int32;
  /** ISO 8601 timestamp of the last build-communities run. Null if never run. */
  lastBuiltAt: utcDateTime | null;
}

model StatusResponse {
  collections: StatusCollectionEntry[];
  version: string;
}

@route("/status")
interface Status {
  @get status(): StatusResponse;
}
