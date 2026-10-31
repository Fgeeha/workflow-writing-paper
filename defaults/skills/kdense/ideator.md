<!-- Источник: K-Dense Scientific Agent Skills, skills/hypothesis-generation
     (https://github.com/K-Dense-AI/scientific-agent-skills, MIT, версия 2026-10-01). Сокращённая выжимка;
     скрипты и шаблоны убраны. Подключается через `system_prefix_file` у провайдера; язык ответа задаёт
     основной промпт роли. -->

# Hypothesis generation (guidance)

A hypothesis is a proposal to be challenged, not a finding. Keep these objects distinct and never
collapse them: observation, research question, hypothesis, mechanism, causal estimand, prediction,
alternative explanation, null hypothesis, negative control, operationalization, analysis plan,
evidence. A mechanistic story is not a prediction; a prediction is not evidence; rejecting one
null does not prove a mechanism; support for one candidate does not eliminate unconsidered rivals.

Workflow:
1. Freeze the observation before interpretation: what was measured, on which population or system,
   unit of observation vs unit of analysis, uncertainty, exclusions, preprocessing, and whether the
   pattern was expected, exploratory or selected after seeing results. Use "reported", "observed",
   "associated"; use causal language only when a causal design and estimand justify it.
2. Frame the research question with a framework only when it fits (PICO/PICOT for interventions,
   PECO for exposures, population-index test-reference standard for diagnostics, construct-context-
   outcome for mechanistic or theoretical work). FINER (feasible, interesting, novel, ethical,
   relevant) is a refinement mnemonic, not a score. Treat "novel" as unresolved until a documented
   search and expert review support it.
3. Establish a dated evidence boundary: what was searched, where, with what cutoff. Say "not
   located within the documented search boundary", never "no prior work exists".
4. Generate rivals before choosing tests. Produce candidates from genuinely different explanatory
   classes: proposed mechanism; measurement or processing artifact; confounding or common cause;
   selection or attrition; conditioning on a collider; reverse causation; temporal, contextual or
   boundary-condition differences; stochastic variation.
5. For each candidate derive discriminating predictions: an observable implication that would
   differ between rivals, the measurement that would show it, and what result would count against
   the candidate. Name negative controls.
6. Draft an analysis plan before data: transformations, model, contrasts, uncertainty, decision
   rules, sample size or precision rationale.

Never: present a hypothesis, mechanism or apparent pattern as established; claim novelty because
a quick search found nothing; infer causation from association or model output; fabricate sources,
data, results or approvals; automatically score, rank, accept or reject hypotheses in place of the
accountable human.
