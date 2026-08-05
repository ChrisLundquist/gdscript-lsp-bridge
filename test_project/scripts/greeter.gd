class_name Greeter
extends RefCounted

## A trivial class used to exercise the language server's symbol indexing.

## The name this greeter addresses people by.
var salutation: String = "Hello"


## Returns a greeting addressed to [param who].
func greet(who: String) -> String:
	return "%s, %s!" % [salutation, who]


## Returns a greeting for everyone in [param names], one per line.
func greet_many(names: Array[String]) -> String:
	var lines: Array[String] = []
	for name_value in names:
		lines.append(greet(name_value))
	return "\n".join(lines)
