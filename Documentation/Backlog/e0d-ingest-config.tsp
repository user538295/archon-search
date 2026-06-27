namespace IngestConfigSeam;

model IngestConfig {
  max_file_mb: int32;
}

interface IngestConfigLoader {
  load(): IngestConfig;
}
