# Semantic shadow v2: first advisory launch

The semantic layer is a **second filter over regex**, never its replacement.
Update82 adds a separate, manually started GitHub Action. It scores an already
archived regex run and uploads a private diagnostic CSV. The daily monitor does
not download a model, score articles, or alter the report.

## Candidate and boundaries

The later full-text A/B diagnostic used the reviewed 74-publication reference
set (35 KEEP, 37 REJECT, 2 BORDERLINE) and compared MiniLM with mDeBERTa. The
compact candidate for the first shadow is
`MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli`: it had the better working
F1 and finished in about a minute, whereas mDeBERTa took about 43 minutes.

This does **not** approve automatic action. At its operating threshold MiniLM
had 26 false inclusions on 72 hard-labelled cards; even its conservative review
bands covered only 4.1% of that reference set. Every shadow result is therefore
advisory and must be reviewed before it is used for a decision. The bands only
order a review queue and are re-checked prospectively before any action is
proposed.

## Data contract

The scheduled workflow persists the private
`debug/semantic_training_signals_*.csv` files under
`data/ml/raw_history/semantic_inputs/`. Each row contains the bounded
`TITLE/SUMMARY/TEXT` input, its hash and the regex decision for both KEEP and
REJECT. These files are internal and are never copied into `reports/`.

## Running the shadow

Open Actions → `Social semantic shadow` → Run workflow. Leave `input_file`
empty to use the newest archived `semantic_inputs` file, or specify one of
those paths. The created `social-semantic-shadow-<run>` artifact contains
`shadow_predictions.csv` and the exact input snapshot. It is private and is
not committed back to the repository.

`VETO_CANDIDATE_REVIEW` and `RESCUE_CANDIDATE_REVIEW` mean only “put this card
in the editor's review queue”; `OBSERVE_ONLY` is not a verdict.

## Next controlled sequence

1. Review shadow disagreements on several ordinary runs without revealing a
   prediction before the editor's initial label.
2. Freeze the hypotheses and the two review bands before using that sample to
   calculate a prospective accuracy result.
3. Draw any next blind sample randomly, event-safely and stratified from the
   archived semantic inputs; do not select it by a known regex error or score.
4. On a prospective blind test require action-level precision at least 90%, no
   harmful rescue of protected/technical/foreign exclusions, and a positive net
   reduction of errors before any automatic action is proposed.
