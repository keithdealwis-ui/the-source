# Intake policy

This is the reviewed policy for The Source's manual intake path (KEI-807). It does for the
intake ledger what an upstream licence file does for a harvested list. `config/sources.yaml`
pins this file by SHA-256. If the file changes, harvest refuses the intake source until the
new text is reviewed and the pin is updated. The policy cannot drift without anyone noticing.

1. **Anyone may suggest a repository; nobody may approve one.** A URL submitted through
   the intake surface is a request for assessment. A human or conversational preliminary
   assessment (for example a ChatGPT review) is advisory. It is never recorded as approval
   and never changes an outcome.
2. **One pipeline.** A submitted repository gets the same live verification, licence rules,
   meaningful-activity rules, archive and identity checks, and Project Health and Evidence
   Confidence scoring as a repository found by scheduled discovery. It is judged against
   `config/policy.yaml` and `config/scoring.yaml`. There is no intake-specific quality bar.
3. **A relationship needs evidence.** The ledger records a SaaS relationship only where the
   project's own public repository (its description, topics or README) says it is an
   alternative to a product in the reviewed SaaS catalogue. The matched text is kept as
   evidence in the outcome record. A submitter's hint about which SaaS it replaces narrows
   the search. It is not evidence.
4. **Facts only.** Like every other source, intake records a project name, a repository
   URL, a declared licence identifier, a category label and the relationship claim. No
   descriptions or README text are republished; matched phrases are kept only as short
   evidence excerpts in the intake outcome record.
5. **Promotion is the normal one.** Admission to the ledger does not make a repository
   canonical. It becomes canonical when the next cycle rebuilds the dataset, the record
   passes dataset validation, the export guard and canonical validation, and the run is
   promoted like any other.
6. **Outcomes are durable.** Every submission ends in exactly one recorded outcome:
   accepted, rejected, already_known, needs_more_evidence or failed. Each carries its
   reasons and evidence. Resubmitting the same request id returns the recorded outcome
   unchanged. A failed assessment can be retried under a new request id.
