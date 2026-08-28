/**
 * Trust role: none. A wrong URL or token here just makes every gateway
 * call fail loudly -- the gateway itself still decides everything.
 */

export interface GatewayConfig {
  url: string;
  token: string;
  region: string;
}

export function readConfig(env: NodeJS.ProcessEnv = process.env): GatewayConfig {
  const url = env.EQUIVALENT_GATEWAY_URL;
  const token = env.EQUIVALENT_GATEWAY_TOKEN;
  const region = env.EQUIVALENT_REGION;
  if (!url || !token || !region) {
    throw new Error(
      "equivalent: set EQUIVALENT_GATEWAY_URL, EQUIVALENT_GATEWAY_TOKEN, and EQUIVALENT_REGION",
    );
  }
  return { url, token, region };
}
