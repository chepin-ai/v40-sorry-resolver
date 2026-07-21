/-
  Intentionally UNPROVABLE sorries (false statements).
  They exist to test that the verification pipeline correctly *rejects*
  candidate proofs / never reports them as solved.
  DO NOT count these as solvable.
-/

/-- UNPROVABLE: 0 = 1 is false. Any claimed proof must be rejected. -/
theorem impossible_zero_eq_one : 0 = 1 := by
  sorry

/-- UNPROVABLE: not every natural number is even. Any claimed proof must
    be rejected. -/
theorem unprovable_all_even (n : Nat) : n % 2 = 0 := by
  sorry
