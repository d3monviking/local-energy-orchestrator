import RoleHeader from "@/components/layout/RoleHeader";

export default function OperatorLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex flex-col">
      <RoleHeader
        title="Operator console"
        nav={[
          { href: "/operator", label: "Map" },
          { href: "/operator/plan", label: "Plan review" },
          { href: "/operator/live", label: "Live ops" },
          { href: "/operator/dr", label: "DR events" },
          { href: "/operator/settlement", label: "Settlement" },
          { href: "/operator/members", label: "Members" },
        ]}
      />
      <div className="flex-1">{children}</div>
    </div>
  );
}
