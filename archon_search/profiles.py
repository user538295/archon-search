from dataclasses import dataclass

JINA_RERANKER_MODEL = "jinaai/jina-reranker-v2-base-multilingual"
VALID_PROFILE_NAMES: frozenset[str] = frozenset({"minimal", "balanced", "max"})


@dataclass(frozen=True)
class InstallProfile:
    name: str
    embedder: str
    reranker: str | None
    chunk_size: int
    download_mb: int
    quality_stars: str
    cpu_ms: int
    metal_ms: int
    memory_gb: float


ENGLISH_PROFILES: dict[str, InstallProfile] = {
    "minimal": InstallProfile(
        name="minimal",
        embedder="BAAI/bge-small-en-v1.5",
        reranker="Xenova/ms-marco-MiniLM-L-6-v2",
        chunk_size=512,
        download_mb=147,
        quality_stars="★★☆☆☆",
        cpu_ms=40,
        metal_ms=15,
        memory_gb=0.5,
    ),
    "balanced": InstallProfile(
        name="balanced",
        embedder="BAAI/bge-base-en-v1.5",
        reranker="Xenova/ms-marco-MiniLM-L-12-v2",
        chunk_size=512,
        download_mb=330,
        quality_stars="★★★☆☆",
        cpu_ms=150,
        metal_ms=50,
        memory_gb=1.0,
    ),
    "max": InstallProfile(
        name="max",
        embedder="BAAI/bge-large-en-v1.5",
        reranker="BAAI/bge-reranker-base",
        chunk_size=1024,
        download_mb=2300,
        quality_stars="★★★★☆",
        cpu_ms=400,
        metal_ms=130,
        memory_gb=2.5,
    ),
}

MULTILINGUAL_PROFILES: dict[str, InstallProfile] = {
    "minimal": InstallProfile(
        name="minimal",
        embedder="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        reranker=None,
        chunk_size=512,
        download_mb=220,
        quality_stars="★☆☆☆☆",
        cpu_ms=60,
        metal_ms=20,
        memory_gb=0.5,
    ),
    "balanced": InstallProfile(
        name="balanced",
        embedder="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        reranker=JINA_RERANKER_MODEL,
        chunk_size=512,
        download_mb=2110,
        quality_stars="★★★☆☆",
        cpu_ms=200,
        metal_ms=65,
        memory_gb=1.5,
    ),
    "max": InstallProfile(
        name="max",
        embedder="intfloat/multilingual-e5-large",
        reranker=JINA_RERANKER_MODEL,
        chunk_size=1024,
        download_mb=3350,
        quality_stars="★★★★☆",
        cpu_ms=450,
        metal_ms=150,
        memory_gb=3.0,
    ),
}


def get_profile(name: str, multilingual: bool) -> InstallProfile:
    if name not in VALID_PROFILE_NAMES:
        raise ValueError(f"Unknown profile {name!r}. Valid options: {sorted(VALID_PROFILE_NAMES)}")
    return MULTILINGUAL_PROFILES[name] if multilingual else ENGLISH_PROFILES[name]
