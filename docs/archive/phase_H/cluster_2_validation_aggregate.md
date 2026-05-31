## Per-run accuracy

| Run | Accuracy | vs baseline (22/50, 44%) |
|---|---|---|
| 1 | 22/50 (44.0%) | +0.0 pp |
| 2 | 20/50 (40.0%) | -4.0 pp |
| 3 | 22/50 (44.0%) | +0.0 pp |
| **avg** | — | **-1.3 pp** |

## Per-rule pass/miss across runs

| rule | run 1 P/M | run 2 P/M | run 3 P/M |
|---|---|---|---|
| NON_STANDARD | 1/4 | 1/4 | 1/4 |
| OVERRIDE | 1/1 | 1/1 | 1/1 |
| R1 | 1/5 | 1/5 | 1/5 |
| R2 | 4/13 | 3/13 | 4/13 |
| R3 | 13/19 | 12/19 | 13/19 |
| R4 | 0/6 | 0/6 | 0/6 |
| R6 | 2/2 | 2/2 | 2/2 |

## Per-case verdict consistency

| case_id | rule | expected | r1 | r2 | r3 | consistent | passed |
|---|---|---|---|---|---|---|---|
| der_abstain_001 | R6 | no_grounding_found | None | None | None | yes | yes |
| der_abstain_002 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_abstain_003 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_abstain_004 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_abstain_005 | R6 | no_grounding_found | None | None | None | yes | yes |
| der_abstain_006 | R3 | verified_given_assertion | no_grounding_found | no_grounding_found | no_grounding_found | yes | no |
| der_cross_001 | R2 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_cross_002 | R3 | verified_given_assertion | verified | verified | verified | yes | no |
| der_cross_003 | R1 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_cross_004 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_cross_005 | R1 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_cross_006 | R1 | verified | verified | verified | verified | yes | yes |
| der_cross_007 | R3 | verified_given_assertion | verified_given_assertion | verified | verified_given_assertion | **NO** | MIXED |
| der_cross_008 | R1 | verified | contradicted | no_grounding_found | no_grounding_found | **NO** | no |
| der_cross_009 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_cross_010 | R1 | no_grounding_found | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_disambiguation_001 | NON_STANDARD | verified_with_correct_entity | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_disambiguation_002 | R2 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_disambiguation_003 | R2 | verified | no_grounding_found | no_grounding_found | no_grounding_found | yes | no |
| der_disambiguation_004 | R2 | verified | no_grounding_found | no_grounding_found | no_grounding_found | yes | no |
| der_disambiguation_005 | NON_STANDARD | <non-standard> | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_disambiguation_006 | NON_STANDARD | verified_with_correct_entity | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_disambiguation_007 | R2 | verified | verified | verified | verified | yes | yes |
| der_disambiguation_008 | R2 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_multihop_001 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_multihop_002 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_multihop_003 | R3 | verified_given_assertion | no_grounding_found | no_grounding_found | no_grounding_found | yes | no |
| der_multihop_004 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_multihop_005 | R2 | verified | verified | verified | verified | yes | yes |
| der_multihop_006 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_multihop_007 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_multihop_008 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_multihop_009 | R3 | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_multihop_010 | R3 | verified_given_assertion | no_grounding_found | no_grounding_found | no_grounding_found | yes | no |
| der_multihop_011 | R2 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_multihop_012 | R3 | verified_given_assertion | verified | verified | verified | yes | no |
| der_predicate_translation_001 | R2 | verified | no_grounding_found | no_grounding_found | no_grounding_found | yes | no |
| der_predicate_translation_002 | OVERRIDE | verified_given_assertion | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | yes |
| der_predicate_translation_003 | R3 | verified_given_assertion | verified | verified | verified | yes | no |
| der_predicate_translation_004 | R2 | verified | verified | verified_given_assertion | verified | **NO** | MIXED |
| der_predicate_translation_005 | R2 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_predicate_translation_006 | R2 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_predicate_translation_007 | NON_STANDARD | needs_tier_u_or_kb | no_grounding_found | no_grounding_found | no_grounding_found | yes | no |
| der_predicate_translation_008 | R2 | verified | verified | verified | verified | yes | yes |
| der_revision_001 | R4 | contradicted | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_revision_002 | R4 | contradicted | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_revision_003 | R4 | contradicted | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_revision_004 | R4 | verified | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |
| der_revision_005 | R4 | contradicted | no_grounding_found | no_grounding_found | no_grounding_found | yes | no |
| der_revision_006 | R4 | contradicted | verified_given_assertion | verified_given_assertion | verified_given_assertion | yes | no |

## Cases with cross-run verdict variance (3)

These cases produced different verdicts across runs — either legitimate KB/extractor nondeterminism or a bug worth investigating.

- **der_cross_007** (R3): verdicts = ['verified_given_assertion', 'verified', 'verified_given_assertion']
- **der_cross_008** (R1): verdicts = ['contradicted', 'no_grounding_found', 'no_grounding_found']
- **der_predicate_translation_004** (R2): verdicts = ['verified', 'verified_given_assertion', 'verified']

Traceback (most recent call last):
  File "C:\code\aedos\scripts\cluster_2_validation_aggregate.py", line 192, in <module>
    sys.exit(main())
             ^^^^^^
  File "C:\code\aedos\scripts\cluster_2_validation_aggregate.py", line 185, in main
    Path(args.out).write_text(json.dumps(agg, indent=2), encoding="utf-8")
                              ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\asash\AppData\Local\Programs\Python\Python311\Lib\json\__init__.py", line 238, in dumps
    **kw).encode(obj)
          ^^^^^^^^^^^
  File "C:\Users\asash\AppData\Local\Programs\Python\Python311\Lib\json\encoder.py", line 202, in encode
    chunks = list(chunks)
             ^^^^^^^^^^^^
  File "C:\Users\asash\AppData\Local\Programs\Python\Python311\Lib\json\encoder.py", line 432, in _iterencode
    yield from _iterencode_dict(o, _current_indent_level)
  File "C:\Users\asash\AppData\Local\Programs\Python\Python311\Lib\json\encoder.py", line 406, in _iterencode_dict
    yield from chunks
  File "C:\Users\asash\AppData\Local\Programs\Python\Python311\Lib\json\encoder.py", line 326, in _iterencode_list
    yield from chunks
  File "C:\Users\asash\AppData\Local\Programs\Python\Python311\Lib\json\encoder.py", line 377, in _iterencode_dict
    raise TypeError(f'keys must be str, int, float, bool or None, '
TypeError: keys must be str, int, float, bool or None, not tuple
