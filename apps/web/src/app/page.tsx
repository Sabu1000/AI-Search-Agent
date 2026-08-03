import { Button } from "@universal-ai-search/ui";

const sources = ["Local files", "Gmail", "Google Drive", "GitHub"];

export default function HomePage() {
  return (
    <main>
      <nav aria-label="Primary navigation">
        <span className="brand">Universal AI Search</span>
        <span className="stage">Foundation build</span>
      </nav>

      <section className="hero">
        <p className="eyebrow">Your knowledge. One trustworthy search.</p>
        <h1>Find the answer—and the source behind it.</h1>
        <p className="lede">
          Connect only what you approve. Search across your work. Receive grounded answers with
          citations you can open and verify.
        </p>
        <div className="actions">
          <Button className="primary" disabled>
            Connect a source
          </Button>
          <span>Account setup arrives in the authentication milestone.</span>
        </div>
      </section>

      <section className="sources" aria-labelledby="source-heading">
        <div>
          <p className="eyebrow">Read-only by design</p>
          <h2 id="source-heading">Search across approved sources</h2>
        </div>
        <ul>
          {sources.map((source) => (
            <li key={source}>{source}</li>
          ))}
        </ul>
      </section>
    </main>
  );
}
