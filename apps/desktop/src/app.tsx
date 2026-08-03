const plannedCapabilities = ["Choose folders", "Review files", "Sync safely"];

export function App() {
  return (
    <main>
      <p className="eyebrow">Desktop foundation</p>
      <h1>Your local files stay under your control.</h1>
      <p className="lede">
        Folder access is opt-in and read-only. Scanning and synchronization arrive in the desktop
        milestone.
      </p>
      <ol>
        {plannedCapabilities.map((capability, index) => (
          <li key={capability}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            {capability}
          </li>
        ))}
      </ol>
    </main>
  );
}
