import pytest, sys
from kb import semantic
Q=[("yakima","and if the panda stuff turns out stale can I bring it back","return_policy"),
   ("yakima","when will it be ready to pick up",""),
   ("yakima","cool, and can I bring some of it back home with me","")]
@pytest.mark.django_db
def test_probe(convo):
    for store,q,topic in Q:
        sys.stdout.write(f"\n=== {q!r} topic={topic!r} cw={sorted(semantic._content_words(q))}\n")
        for row, sc in semantic.rank_faq(q, store=store, top_k=5, topic=topic):
            sys.stdout.write(f" {sc:7.3f} rel={semantic.relevant_enough(q,row)} {type(row).__name__} {semantic._stable_sort_key(row)!r} :: {row.chunk_text()[:130].encode('ascii','replace').decode()}\n")
