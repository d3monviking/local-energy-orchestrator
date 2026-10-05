export default function Home() {
  return (
    <main id="main-content" style={{ fontFamily: "system-ui", padding: "2rem" }}>
      <h1>LEO</h1>
      <p>Local Energy Orchestrator. <a href="/login">Sign in</a> to reach any surface.</p>
      <ul>
        <li><a href="/operator">/operator</a> — map/timeline, plan review, live ops, DR events, settlement, members</li>
        <li><a href="/citizen">/citizen</a> — offers, earnings, usage, consent, outage status, community</li>
        <li><a href="/discom">/discom</a> — action queue, verified flexibility, DFPO progress</li>
      </ul>
    </main>
  );
}
