# recipes/

One page per recipe. Give each one YAML frontmatter, because the directory's
index is built from it:

```yaml
---
title: Lentil Soup
cuisine: vegetarian
time: 40m
serves: 4
---
```

`title`, `cuisine`, `time` and `serves` are what `VIEW.md` reads. A recipe
missing one still appears in the index — the cell shows what the spec says an
empty value means, never a silent blank.

Below the frontmatter, write the recipe as prose: a sentence on what it is and
when to make it, then ingredients, then method.
