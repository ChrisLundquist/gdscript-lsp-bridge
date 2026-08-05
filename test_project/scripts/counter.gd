class_name Counter
extends RefCounted

## A trivial accumulator used to exercise cross-file references.

var _total: int = 0


## Adds [param amount] to the running total and returns the new total.
func add(amount: int) -> int:
	_total += amount
	return _total


## Returns the running total without modifying it.
func total() -> int:
	return _total


## Resets the running total to zero.
func reset() -> void:
	_total = 0
