# Dependencies

Every library you added to `requirements-extra.txt`, with a one-line reason.

We are not counting libraries — a well-chosen dependency is better engineering than a
hand-rolled version of the same thing. What we are reading is whether you added each
one deliberately.

| Library | Version | Why |
|---|---|---|
| *example* | *2.1.0* | *replace this row* |

## Anything you considered and rejected

I did not add any additional Python dependencies beyond those already provided by the challenge.

## Note

I avoided adding a 13F-specific retrieval or parsing library because I wanted the filing retrieval, XML parsing, schema handling, and edge cases to remain explicit in my own pipeline.

For local Chapter 4 testing, I used Ollama outside the submitted Python dependency set. It was only used as a local model server and is not required by the grading pipeline.
