namespace IngestErrorSeam;

enum IngestErrorCode {
  file_too_large,
}

model FileTooLargeError {
  code: IngestErrorCode;
  message: string;
}

model IngestResultExtension {
  code?: IngestErrorCode;
}

interface IngestErrorRaiser {
  checkFileSize(path: string, maxFileMb: int32): void;
}
