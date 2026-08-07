type RuntimeConfig = {
  apiBaseUrl?: string;
  backendPort?: number | string;
  userId?: string;
};

const DEFAULT_BACKEND_PORT = 8010;
const DEFAULT_USER_ID = "debug-user";

function parsePort(value: number | string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function normalizeBaseUrl(value: string | undefined | null): string {
  return (value ?? "").trim().replace(/\/+$/, "");
}

function readRuntimeConfig(): RuntimeConfig {
  if (typeof window === "undefined") {
    return {};
  }
  return window.__HAILIANG_RUNTIME_CONFIG__ ?? {};
}

function detectBackendPort(): number {
  const runtimeConfig = readRuntimeConfig();
  return parsePort(
    runtimeConfig.backendPort ?? import.meta.env.VITE_BACKEND_PORT,
    DEFAULT_BACKEND_PORT,
  );
}

function detectHostname(): string {
  if (typeof window === "undefined") {
    return "127.0.0.1";
  }
  return window.location.hostname || "127.0.0.1";
}

function detectProtocol(): string {
  if (typeof window === "undefined") {
    return "http:";
  }
  return window.location.protocol === "https:" ? "https:" : "http:";
}

export function getAutoDetectedApiBaseUrl(): string {
  return `${detectProtocol()}//${detectHostname()}:${detectBackendPort()}`;
}

export function getRuntimeApiBaseUrl(): string {
  const runtimeConfig = readRuntimeConfig();
  const configuredBaseUrl = normalizeBaseUrl(
    runtimeConfig.apiBaseUrl ?? import.meta.env.VITE_API_BASE_URL,
  );
  return configuredBaseUrl || getAutoDetectedApiBaseUrl();
}

export function getRuntimeUserId(): string {
  const runtimeConfig = readRuntimeConfig();
  return runtimeConfig.userId?.trim() || import.meta.env.VITE_DEFAULT_USER_ID || DEFAULT_USER_ID;
}
