/-
  Trivial sorries: provable with one elementary tactic
  (rfl / decide / exact / simp). No mathlib required.
-/

/-- TRIVIAL: reflexivity. Candidate proof: `rfl` -/
theorem nat_refl (n : Nat) : n = n := by
  sorry

/-- TRIVIAL: numeral computation. Candidate proof: `rfl` or `decide` -/
theorem one_plus_one : 1 + 1 = 2 := by
  sorry

/-- TRIVIAL: swap a conjunction. Candidate proof: `exact ⟨h.2, h.1⟩` -/
theorem and_comm_simple (a b : Prop) (h : a ∧ b) : b ∧ a := by
  sorry

/-- TRIVIAL: left injection into a disjunction. Candidate proof: `exact Or.inl h` -/
theorem or_intro_simple (p q : Prop) (h : p) : p ∨ q := by
  sorry

/-- TRIVIAL: length of append; `List.length_append` is a core `@[simp]` lemma.
    Candidate proof: `simp` -/
theorem list_length_append_simple (xs ys : List Nat) :
    (xs ++ ys).length = xs.length + ys.length := by
  sorry
