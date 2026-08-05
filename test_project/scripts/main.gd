extends Node

## Entry point that references both Greeter and Counter so the language server
## has real cross-file references to resolve.

var _greeter: Greeter = Greeter.new()
var _counter: Counter = Counter.new()


func _ready() -> void:
	print(_greeter.greet("world"))
	print(_greeter.greet_many(["a", "b"]))
	_counter.add(1)
	_counter.add(2)
	print(_counter.total())
	_counter.reset()
	report()


## Prints a one-line summary built from both helper classes.
func report() -> void:
	var summary_greeter: Greeter = Greeter.new()
	summary_greeter.salutation = "Report"
	print(summary_greeter.greet(str(_counter.total())))
