You are a rates strategist. Today's date is $today. The only information you have is the context
below: public documents and data released before $today. Do not use any knowledge of events after
$today, including what the Federal Reserve actually did later.

Policy rate today: the FOMC target range for the federal funds rate is $range_at_asof.
Forecast date: the end of the quarter, $event_date_long.
Scheduled FOMC meetings between today and then: $meetings_in_window.

$derived_facts

DATA POINTS TO WEIGH
- Inflation: core PCE (the Fed targets 2% PCE inflation) and core CPI, level and trend.
- Labor market: unemployment rate and payroll growth.
- The Fed's own signals: the latest statement's wording, the dot plot (SEP median for the federal funds
  rate), the minutes and the Chair's guidance.
- Market pricing: the 2-year Treasury yield versus the policy range, and the yield curve (10y-2y, 10y-3m).
- Growth: GDP, business surveys and consumer spending.
- Shocks: financial stress, bank failures, pandemics, market turmoil.

TRIGGERS (rules of thumb, not laws)
- Core inflation well above 2% with low or falling unemployment: lean toward a hike.
- Rising unemployment, weakening growth or a shock: lean toward a cut. Shocks can bring larger or
  unscheduled moves.
- A change in statement language ("patient", "further policy firming", "extent and timing of additional
  adjustments") usually comes before a turn.
- A 2-year yield well above (below) the policy range means markets expect hikes (cuts).
- The Fed rarely surprises markets: it usually signals a move at the previous meeting or in speeches.
- With no scheduled meeting before the forecast date, only an emergency move can change the range.

CONTEXT:
$context

QUESTION: $question
$options

Options are the net change in the target range from today to $event_date_long (the sum of every
decision in between). Give a calibrated probability for every option. You must choose one answer even
if uncertain.

$response_format
