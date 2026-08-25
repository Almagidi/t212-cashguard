import { AlertTriangle } from "lucide-react";

import { Card, CardContent } from "@/components/ui";
import type { PortfolioBacktestJob } from "@/types";

export function PortfolioEvidenceCard({ job }: { job: PortfolioBacktestJob }) {
  if (job.verdict !== "insufficient_evidence") return null;

  const coverage = job.coverage;
  const droppedCount = coverage?.dropped_session_ids.length ?? 0;

  return (
    <Card className="border-amber-500/30" role="status">
      <CardContent className="flex items-start gap-3 p-6">
        <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" />
        <div className="space-y-2">
          <div>
            <p className="text-sm font-semibold text-amber-400">
              Insufficient Portfolio Evidence
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              The strategy was not evaluated because the historical session
              evidence did not satisfy the configured coverage policy.
            </p>
          </div>
          {coverage && (
            <div className="text-xs text-muted-foreground">
              <p className="font-medium text-foreground">
                {coverage.retained_coverage_pct.toFixed(2)}% retained
              </p>
              <p>
                {droppedCount} expected{" "}
                {droppedCount === 1 ? "session was" : "sessions were"} dropped
                across the {coverage.calendar} universe alignment.
              </p>
            </div>
          )}
          {!!job.evidence_reasons?.length && (
            <ul className="list-disc space-y-1 pl-4 text-xs text-amber-300/90">
              {job.evidence_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
