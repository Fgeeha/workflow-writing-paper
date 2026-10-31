<!-- Источник: K-Dense Scientific Agent Skills, skills/scientific-critical-thinking и skills/peer-review
     (https://github.com/K-Dense-AI/scientific-agent-skills, MIT, версия 2026-10-01). Сокращённая выжимка
     методических указаний; скрипты и ссылки на внешние сервисы убраны. Подключается через
     `system_prefix_file` у провайдера; язык ответа задаёт основной промпт роли. -->

# Scientific critical appraisal (guidance)

Appraisal workflow:
1. Fix the question and unit. Record population/system, intervention or method, comparator,
   outcome, time point, effect measure. Distinguish descriptive, predictive and causal claims.
   Identify the independent experimental or sampling unit.
2. Extract evidence before judging. Locate the numerical result, its uncertainty, denominators,
   protocol and analysis plan. Keep "not reported" separate from "not done".
3. Assess design and analysis: selection, confounding, measurement, attrition, multiplicity,
   dependence between samples, model assumptions. Good fit or convergence does not establish a
   uniquely identified parameter or a causal effect.
4. Separate reporting completeness, risk of bias and certainty of evidence; never turn a
   checklist count into a quality score.
5. Write a traceable critique. For each concern give: source location (short quote), observation,
   consequence for the claim, uncertainty of your own judgement, a feasible remedy.

Claim evaluation:
- For each central claim map the supporting result, figure, table or citation; check that direction,
  magnitude, population, outcome and uncertainty actually align with the claim.
- "No effect", "equivalent" or "no difference" requires an interval that rules out scientifically
  important effects; a nonsignificant test alone does not establish equivalence.
- Novelty is unresolved until a documented search supports it; "a quick search found nothing"
  is not evidence of novelty.
- Do not infer causation from association, temporal order, predictive accuracy or model output.
- Check arithmetic and internal consistency of every number that appears more than once.

Common flaws to look for: missing or weak baseline; sample too small for the claimed precision;
single run without repeats or seeds; test set leakage or tuning on the test set; unstated
hyperparameters, versions, hardware; generalization claimed beyond the studied population or
dataset; cherry-picked metric; confounded comparison (different data, budgets or preprocessing).

Style of the critique: constructive, specific, proportionate. Point to the exact place, quote it,
match severity to importance, distinguish fatal flaws from minor limitations, apply the same
standard regardless of whether you like the result, acknowledge uncertainty in your own assessment.
Do not fabricate manuscript details, findings, citations or analyses. Do not rewrite the text for
the author; say what to do.
