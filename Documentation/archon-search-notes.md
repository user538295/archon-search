# archon-search notes

## Feature ideas

### The user can search between the visited websites and if he asks questions in a topic, then these websites also could help him to recall and use those information.

A chrome extension could be written to use it to ingest the current website content and link to be able to use later and be indexed. In this case a new type of sources could be added to the archon-search. A challange can be if it is stored in one collections, because many various type of contents would be stored in one collection (from cooking recipes to technical deep dive in documentation and other researches). It could be hard to find the right collection for the app.

### Link frontier model chat history into archon-search

It would be nice if the user could search in the cloud AI chat history as well. Many times the users use more AI providers in the same topic and in this case we could connect the similar topic into one merged search. For example is the user develops an app like Financialwell.app and he has chats in two providers like Claude and ChatGPT, it would be powerfull if the user or another LLM could use this search and could have a hollistic view and access to these contens in this topic. The user don't need to remember wher does this topic have discussed before, just talk wiht his LLM and the LLM will remember (find) the right conversation and use it.
We should ingest the whole chat history from the beginning. Need to solve the sync and handle rate limits as well.
First we should support ChatGPT, Claude and Perplexity. Is there any services to be able to have access all of them via one MCP, API interface?

### Handle 30+ collections better

If we have more collections in the future, then we should think over the handling of the search between collections. Now we have a heuristic, to determine in which collections should be search (not all of them), but later we should handle it even better. Eg.: What if we put another layer to collection search? The collections should have a larger more detailed description about what are in that collection and run a very fast and effective search on these descriptions to determine which collections will fit the best. We should define what does the more detailed collection mean. I expect that the 

### Flaky tests

The tests are flaky after we boost and make to run parallel the tests. Investigate the jsonl logs to find evidence for the flake tests and make an extremely deep investigation what cause this and how to solve them. Don't fix it yet.
"Pre-existing install-lock parallel flake (the task 2.3 subagent already flagged it — ~/.archon-search/.install.lock is hardcoded Path.home() outside this task's scope). Re-running full suite"

