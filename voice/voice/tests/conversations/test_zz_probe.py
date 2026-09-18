import pytest, sys
from kb import semantic
@pytest.mark.django_db
def test_probe(convo):
    for store, q in [("pullman","hi, it's my first time coming in - what ID do I need to bring?"),
                     ("yakima","perfect, thanks so much")]:
        c = convo(store=store); t = c.say(q)
        sys.stdout.write(f"\n### {store} {q!r}\n intent={t.intent} grounded={t.grounded} src={[s.get('title') for s in t.sources]}\n ans={t.answer[:250].encode('ascii','replace').decode()!r}\n")
        for row, sc in semantic.rank_faq(q, store=store, top_k=4):
            sys.stdout.write(f"   {sc:7.3f} rel={semantic.relevant_enough(q,row)} {semantic._stable_sort_key(row)!r}\n")
