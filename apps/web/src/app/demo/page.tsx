/**
 * Where the made-up cohort lives (D-279).
 *
 * The screens were built against a synthetic cohort - made-up securities,
 * prices, disclosures and answers, run through the real Phase B code with a
 * fake for every provider - so that they could be checked before anything real
 * existed. It is kept, because a screen with no data is hard to judge, but it
 * is not the system: it sits here, behind its own link, and says what it is on
 * every page.
 */

import Link from "next/link";
import { SHADOW_TABS, ShadowNotConfigured, openConfigured } from "@/components/ShadowChrome";
import { isSynthetic } from "@/lib/shadow/model";

export const dynamic = "force-dynamic";

export default async function Demo() {
  const opened = await openConfigured("demo");
  if ("problem" in opened) return <ShadowNotConfigured problem={opened.problem} />;
  const { shadow } = opened;
  const fixture = shadow.fixture;

  return (
    <>
      <div className="notice stop">
        <strong>Made-up data.</strong> Nothing under this link is a real security, price, disclosure or answer.
        The synthetic cohort <span className="mono">{shadow.cohortId}</span> was generated so the screens could
        be built and checked; it is run through the real code with fakes for every provider, as of{" "}
        {fixture ? new Date(fixture.as_of).toISOString().slice(0, 16).replace("T", " ") : "its own clock"}.
        {!isSynthetic(shadow) ? " (This cohort carries no synthetic marker: check what is configured.)" : null}
      </div>

      <section className="panel">
        <h2>The synthetic cohort</h2>
        <p className="hint" style={{ marginTop: 0 }}>
          Every screen below reads it the way it will read a real one: artifacts from the store, each against
          its integrity record. What is real is <Link href="/">the system</Link>: the deployment, the checks,
          the rehearsal and the runs.
        </p>
        <ul>
          {SHADOW_TABS.map((tab) => (
            <li key={tab.href}>
              <Link href={`${tab.href}?cohort=demo`}>{tab.label}</Link>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
