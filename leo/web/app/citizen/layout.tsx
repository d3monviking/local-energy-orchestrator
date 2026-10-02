import RoleHeader from "@/components/layout/RoleHeader";

export default function CitizenLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex flex-col">
      <RoleHeader title="Citizen app" />
      <div className="flex-1">{children}</div>
    </div>
  );
}
