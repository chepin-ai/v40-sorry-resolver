/-
  Medium sorries: need `omega` (core tactic, no mathlib) or a short
  induction combined with simp.
-/

/-- MEDIUM: right additive identity. Candidate proof: `omega`
    (or ` induction n <;> simp [Nat.add_succ]`). -/
theorem add_zero_custom (n : Nat) : n + 0 = n := by
  sorry

/-- MEDIUM: commutativity of addition on Nat. Candidate proof: `omega`. -/
theorem add_comm_custom (a b : Nat) : a + b = b + a := by
  sorry

/-- MEDIUM: multiplication by a literal is linear. Candidate proof: `omega`. -/
theorem mul_two (n : Nat) : n * 2 = n + n := by
  sorry

/-- MEDIUM: mapping the identity function does nothing.
    Candidate proof: `induction xs with
    | nil => rfl
    | cons x xs ih => simp [List.map_cons, ih]` (or `simp` may close it
    via the core simp set on some versions). -/
theorem list_map_id (xs : List Nat) : xs.map id = xs := by
  sorry
