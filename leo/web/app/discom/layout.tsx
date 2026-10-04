import RoleHeader from "@/components/layout/RoleHeader";

export default function DiscomLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex flex-col">
      <RoleHeader
        title="DISCOM dashboard"
        nav={[
          { href: "/discom", label: "Feeder" },
          { href: "/discom/impact", label: "Impact & economics" },
        ]}
      />
      <div className="flex-1">{children}</div>
    </div>
  );
}
