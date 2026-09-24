import { SectionPlaceholder } from "@pixel-console/components/placeholder";

export default function Page() {
  return <SectionPlaceholder title="Settings" permission="organization.manage"
    description="Organization profile, login policy, credentials, billing and retention." />;
}
