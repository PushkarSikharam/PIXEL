import { SectionPlaceholder } from "@pixel-console/components/placeholder";

export default function Page() {
  return <SectionPlaceholder title="Audit" permission="audit.read"
    description="The append-only record of privileged actions: who did what to which resource, with identifiers only." />;
}
