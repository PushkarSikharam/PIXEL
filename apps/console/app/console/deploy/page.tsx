import { SectionPlaceholder } from "@pixel-console/components/placeholder";

export default function Page() {
  return <SectionPlaceholder title="Deploy" permission="deployments.read" phase="SaaS Phase 7"
    description="Releases, compatibility checks, rollout and rollback per environment. Rollback is already prototyped on each product's page." />;
}
