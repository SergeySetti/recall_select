# AI Agent Memory and Vector Databases

AI agents generally have two kinds of memory: short-term and long-term. Sometimes people also single out working memory - the fleeting kind that lives inside the neural network itself during generation, the neurons firing in the moment - but it rarely comes up when we talk about memory as such. There's also a sort of "mid-term" memory - a running summary stretching from the present back to some point in the past. That works nicely too, provided you bake it thoughtfully into the architecture. Most of the time, though, it comes down to one of two setups: either the session alone serves as memory (just the list of past messages), or the session gets paired with long-term memory kept in a database.

# Vector Databases

There are plenty of them. Let me google around and reel off a huge list, just to revel in the sheer variety of these solutions and the beauty of their names.

- Weaviate
- Milvus
- Pinecone
- Qdrant
- Vespa
- Redis Vector Search
- Vald
- Chroma
- Typesense
- Vearch
- pgvector
- LanceDB
- MyScale
- OpenSearch
- sqlite-vec

Every one of these is a wonderful piece of engineering built around a single idea borrowed from linear algebra: placing points in a high-dimensional space of meanings so you can then run operations on them. And the results of those operations are the results of doing math with meaning itself. Just sit with that for a second! Isn't it beautiful? I'm a huge fan of multidimensional meaning-spaces, so this whole topic is near and dear to me.

# Obsidian

Obsidian does have a plugin for vectorizing text. But hardly anyone actually uses it. And yet, for some reason, the entire internet stampeded to wire Obsidian up as long-term memory for AI agents. Karpathy floated the idea, and since most people don't really grasp what's going on under the hood, they swallowed the workaround whole, without a shred of critical thinking. It probably helped that Obsidian renders such a pretty graph.

But Obsidian simply can't handle large collections of memories efficiently. The agent has to read through all the notes, over and over, just to find the one thing it needs. And once you have a lot of notes, that turns into a real problem. Vector databases fix this by letting the agent pull up the right memory instantly, with no need to plow through every note - even when there are an absurd number of them.

# Fuzzy Search

But here's the crux: Obsidian can't search by meaning. It searches by keywords. Vector databases, on the other hand, let you search by meaning, which is far more powerful. Say you want to recall which Italian dessert we were talking about - in Obsidian you'd have to remember the keyword "dessert" or "Italian," and those words might not be in the note at all. All it might say is "tiramisu." Karpathy cursed us all with that Obsidian idea. I'm sure he's regretted it a thousand times over by now. On top of that, having the agent constantly re-read its entire memory out of Obsidian burns an enormous amount of tokens. Vector databases let you search by meaning and load into the agent's mind only what's genuinely close to the query. That saves tokens and speeds the agent up. And it happens blazingly fast, because vector databases are built and tuned for exactly this kind of operation.

# How

The simplest way to plug in semantic vector memory is to grab a small cloud-hosted collection through a service like **recall.select** and hand it to your agent to connect. It's honestly a three-click affair.

Alternatively, even SQLite has vector extensions these days - already a big step up from stashing everything in plain files or in Obsidian.
