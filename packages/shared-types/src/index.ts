import { z } from "zod";

export const serviceStatusSchema = z.enum(["ok", "degraded"]);

export const healthResponseSchema = z.object({
  service: z.string().min(1),
  status: serviceStatusSchema,
  version: z.string().min(1),
});

export type HealthResponse = z.infer<typeof healthResponseSchema>;
export type ServiceStatus = z.infer<typeof serviceStatusSchema>;
