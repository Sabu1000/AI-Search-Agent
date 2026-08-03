import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  transpilePackages: ["@universal-ai-search/shared-types", "@universal-ai-search/ui"],
};

export default nextConfig;
