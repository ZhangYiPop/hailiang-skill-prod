/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_BACKEND_PORT?: string;
  readonly VITE_DEFAULT_USER_ID?: string;
  readonly VITE_API_RETRY_COUNT?: string;
  readonly VITE_API_RETRY_DELAY_MS?: string;
  readonly VITE_API_TIMEOUT_MS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface Window {
  __HAILIANG_RUNTIME_CONFIG__?: {
    apiBaseUrl?: string;
    backendPort?: number | string;
    userId?: string;
  };
}
