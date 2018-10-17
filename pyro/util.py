from __future__ import absolute_import, division, print_function

import functools
import numbers
import random
import warnings
from collections import defaultdict
from contextlib import contextmanager

import graphviz
import torch
from six.moves import zip_longest

from pyro.poutine.util import site_is_subsample


def set_rng_seed(rng_seed):
    """
    Sets seeds of torch and torch.cuda (if available).
    :param int rng_seed: The seed value.
    """
    torch.manual_seed(rng_seed)
    random.seed(rng_seed)
    try:
        import numpy as np
        np.random.seed(rng_seed)
    except ImportError:
        pass


def torch_isnan(x):
    """
    A convenient function to check if a Tensor contains any nan; also works with numbers
    """
    if isinstance(x, numbers.Number):
        return x != x
    return torch.isnan(x).any()


def torch_isinf(x):
    """
    A convenient function to check if a Tensor contains any +inf; also works with numbers
    """
    if isinstance(x, numbers.Number):
        return x == float('inf') or x == -float('inf')
    return (x == float('inf')).any() or (x == -float('inf')).any()


def warn_if_nan(value, msg=""):
    """
    A convenient function to warn if a Tensor or its grad contains any nan,
    also works with numbers.
    """
    if torch.is_tensor(value) and value.requires_grad:
        value.register_hook(lambda x: warn_if_nan(x, msg))
    if torch_isnan(value):
        warnings.warn("Encountered NaN{}".format((': ' if msg else '.') + msg), stacklevel=2)


def warn_if_inf(value, msg="", allow_posinf=False, allow_neginf=False):
    """
    A convenient function to warn if a Tensor or its grad contains any inf,
    also works with numbers.
    """
    if torch.is_tensor(value) and value.requires_grad:
            value.register_hook(lambda x: warn_if_inf(x, msg, allow_posinf, allow_neginf))
    if (not allow_posinf) and (value == float('inf') if isinstance(value, numbers.Number)
                               else (value == float('inf')).any()):
        warnings.warn("Encountered +inf{}".format((': ' if msg else '.') + msg), stacklevel=2)
    if (not allow_neginf) and (value == -float('inf') if isinstance(value, numbers.Number)
                               else (value == -float('inf')).any()):
        warnings.warn("Encountered -inf{}".format((': ' if msg else '.') + msg), stacklevel=2)


def save_visualization(trace, graph_output):
    """
    :param pyro.poutine.Trace trace: a trace to be visualized
    :param graph_output: the graph will be saved to graph_output.pdf
    :type graph_output: str

    Take a trace generated by poutine.trace with `graph_type='dense'` and render
    the graph with the output saved to file.

    - non-reparameterized stochastic nodes are salmon
    - reparameterized stochastic nodes are half salmon, half grey
    - observation nodes are green

    Example:

    trace = pyro.poutine.trace(model, graph_type="dense").get_trace()
    save_visualization(trace, 'output')
    """
    g = graphviz.Digraph()

    for label, node in trace.nodes.items():
        if site_is_subsample(node):
            continue
        shape = 'ellipse'
        if label in trace.stochastic_nodes and label not in trace.reparameterized_nodes:
            fillcolor = 'salmon'
        elif label in trace.reparameterized_nodes:
            fillcolor = 'lightgrey;.5:salmon'
        elif label in trace.observation_nodes:
            fillcolor = 'darkolivegreen3'
        else:
            # only visualize RVs
            continue
        g.node(label, label=label, shape=shape, style='filled', fillcolor=fillcolor)

    for label1, label2 in trace.edges:
        if site_is_subsample(trace.nodes[label1]):
            continue
        if site_is_subsample(trace.nodes[label2]):
            continue
        g.edge(label1, label2)

    g.render(graph_output, view=False, cleanup=True)


def check_traces_match(trace1, trace2):
    """
    :param pyro.poutine.Trace trace1: Trace object of the model
    :param pyro.poutine.Trace trace2: Trace object of the guide
    :raises: RuntimeWarning, ValueError

    Checks that (1) there is a bijection between the samples in the two traces
    and (2) at each sample site two traces agree on sample shape.
    """
    # Check ordinary sample sites.
    vars1 = set(name for name, site in trace1.nodes.items() if site["type"] == "sample")
    vars2 = set(name for name, site in trace2.nodes.items() if site["type"] == "sample")
    if vars1 != vars2:
        warnings.warn("Model vars changed: {} vs {}".format(vars1, vars2))

    # Check shapes agree.
    for name in vars1:
        site1 = trace1.nodes[name]
        site2 = trace2.nodes[name]
        if hasattr(site1["fn"], "shape") and hasattr(site2["fn"], "shape"):
            shape1 = site1["fn"].shape(*site1["args"], **site1["kwargs"])
            shape2 = site2["fn"].shape(*site2["args"], **site2["kwargs"])
            if shape1 != shape2:
                raise ValueError("Site dims disagree at site '{}': {} vs {}".format(name, shape1, shape2))


def check_model_guide_match(model_trace, guide_trace, max_plate_nesting=float('inf')):
    """
    :param pyro.poutine.Trace model_trace: Trace object of the model
    :param pyro.poutine.Trace guide_trace: Trace object of the guide
    :raises: RuntimeWarning, ValueError

    Checks the following assumptions:
    1. Each sample site in the model also appears in the guide and is not
        marked auxiliary.
    2. Each sample site in the guide either appears in the model or is marked,
        auxiliary via ``infer={'is_auxiliary': True}``.
    3. Each :class:``~pyro.plate`` statement in the guide also appears in the
        model.
    4. At each sample site that appears in both the model and guide, the model
        and guide agree on sample shape.
    """
    # Check ordinary sample sites.
    guide_vars = set(name for name, site in guide_trace.nodes.items()
                     if site["type"] == "sample"
                     if type(site["fn"]).__name__ != "_Subsample")
    aux_vars = set(name for name, site in guide_trace.nodes.items()
                   if site["type"] == "sample"
                   if site["infer"].get("is_auxiliary"))
    model_vars = set(name for name, site in model_trace.nodes.items()
                     if site["type"] == "sample" and not site["is_observed"]
                     if type(site["fn"]).__name__ != "_Subsample")
    enum_vars = set(name for name, site in model_trace.nodes.items()
                    if site["type"] == "sample" and not site["is_observed"]
                    if type(site["fn"]).__name__ != "_Subsample"
                    if site["infer"].get("_enumerate_dim") is not None
                    if name not in guide_vars)
    if aux_vars & model_vars:
        warnings.warn("Found auxiliary vars in the model: {}".format(aux_vars & model_vars))
    if not (guide_vars <= model_vars | aux_vars):
        warnings.warn("Found non-auxiliary vars in guide but not model, "
                      "consider marking these infer={{'is_auxiliary': True}}:\n{}".format(
                          guide_vars - aux_vars - model_vars))
    if not (model_vars <= guide_vars | enum_vars):
        warnings.warn("Found vars in model but not guide: {}".format(model_vars - guide_vars - enum_vars))

    # Check shapes agree.
    for name in model_vars & guide_vars:
        model_site = model_trace.nodes[name]
        guide_site = guide_trace.nodes[name]

        if hasattr(model_site["fn"], "event_dim") and hasattr(guide_site["fn"], "event_dim"):
            if model_site["fn"].event_dim != guide_site["fn"].event_dim:
                raise ValueError("Model and guide event_dims disagree at site '{}': {} vs {}".format(
                    name, model_site["fn"].event_dim, guide_site["fn"].event_dim))

        if hasattr(model_site["fn"], "shape") and hasattr(guide_site["fn"], "shape"):
            model_shape = model_site["fn"].shape(*model_site["args"], **model_site["kwargs"])
            guide_shape = guide_site["fn"].shape(*guide_site["args"], **guide_site["kwargs"])
            if model_shape == guide_shape:
                continue

            # Allow broadcasting outside of max_plate_nesting.
            if len(model_shape) > max_plate_nesting:
                model_shape = model_shape[len(model_shape) - max_plate_nesting:]
            if len(guide_shape) > max_plate_nesting:
                guide_shape = guide_shape[len(guide_shape) - max_plate_nesting:]
            if model_shape == guide_shape:
                continue
            for model_size, guide_size in zip_longest(reversed(model_shape), reversed(guide_shape), fillvalue=1):
                if model_size != guide_size:
                    raise ValueError("Model and guide shapes disagree at site '{}': {} vs {}".format(
                        name, model_shape, guide_shape))

    # Check subsample sites introduced by plate.
    model_vars = set(name for name, site in model_trace.nodes.items()
                     if site["type"] == "sample" and not site["is_observed"]
                     if type(site["fn"]).__name__ == "_Subsample")
    guide_vars = set(name for name, site in guide_trace.nodes.items()
                     if site["type"] == "sample"
                     if type(site["fn"]).__name__ == "_Subsample")
    if not (guide_vars <= model_vars):
        warnings.warn("Found plate statements in guide but not model: {}".format(guide_vars - model_vars))


def check_site_shape(site, max_plate_nesting):
    actual_shape = list(site["log_prob"].shape)

    # Compute expected shape.
    expected_shape = []
    for f in site["cond_indep_stack"]:
        if f.dim is not None:
            # Use the specified plate dimension, which counts from the right.
            assert f.dim < 0
            if len(expected_shape) < -f.dim:
                expected_shape = [None] * (-f.dim - len(expected_shape)) + expected_shape
            if expected_shape[f.dim] is not None:
                raise ValueError('\n  '.join([
                    'at site "{}" within plate("", dim={}), dim collision'.format(site["name"], f.name, f.dim),
                    'Try setting dim arg in other plates.']))
            expected_shape[f.dim] = f.size
    expected_shape = [-1 if e is None else e for e in expected_shape]

    # Check for plate stack overflow.
    if len(expected_shape) > max_plate_nesting:
        raise ValueError('\n  '.join([
            'at site "{}", plate stack overflow'.format(site["name"]),
            'Try increasing max_plate_nesting to at least {}'.format(len(expected_shape))]))

    # Ignore dimensions left of max_plate_nesting.
    if max_plate_nesting < len(actual_shape):
        actual_shape = actual_shape[len(actual_shape) - max_plate_nesting:]

    # Check for incorrect plate placement on the right of max_plate_nesting.
    for actual_size, expected_size in zip_longest(reversed(actual_shape), reversed(expected_shape), fillvalue=1):
        if expected_size != -1 and expected_size != actual_size:
            raise ValueError('\n  '.join([
                'at site "{}", invalid log_prob shape'.format(site["name"]),
                'Expected {}, actual {}'.format(expected_shape, actual_shape),
                'Try one of the following fixes:',
                '- enclose the batched tensor in a with plate(...): context',
                '- .independent(...) the distribution being sampled',
                '- .permute() data dimensions']))

    # TODO Check parallel dimensions on the left of max_plate_nesting.


def _are_independent(counters1, counters2):
    for name, counter1 in counters1.items():
        if name in counters2:
            if counters2[name] != counter1:
                return True
    return False


def check_traceenum_requirements(model_trace, guide_trace):
    """
    Warn if user could easily rewrite the model or guide in a way that would
    clearly avoid invalid dependencies on enumerated variables.

    :class:`~pyro.infer.traceenum_elbo.TraceEnum_ELBO` enumerates over
    synchronized products rather than full cartesian products. Therefore models
    must ensure that no variable outside of an plate depends on an enumerated
    variable inside that plate. Since full dependency checking is impossible,
    this function aims to warn only in cases where models can be easily
    rewitten to be obviously correct.
    """
    enumerated_sites = set(name for name, site in guide_trace.nodes.items()
                           if site["type"] == "sample" and site["infer"].get("enumerate"))
    for role, trace in [('model', model_trace), ('guide', guide_trace)]:
        irange_counters = {}
        enumerated_contexts = defaultdict(set)
        for name, site in trace.nodes.items():
            if site["type"] != "sample":
                continue
            irange_counter = {f.name: f.counter for f in site["cond_indep_stack"] if not f.vectorized}
            context = frozenset(f for f in site["cond_indep_stack"] if f.vectorized)

            # Check that sites outside each independence context precede enumerated sites inside that context.
            for enumerated_context, names in enumerated_contexts.items():
                if not (context < enumerated_context):
                    continue
                names = sorted(n for n in names if not _are_independent(irange_counter, irange_counters[n]))
                if not names:
                    continue
                diff = sorted(f.name for f in enumerated_context - context)
                warnings.warn('\n  '.join([
                    'at {} site "{}", possibly invalid dependency.'.format(role, name),
                    'Expected site "{}" to precede sites "{}"'.format(name, '", "'.join(sorted(names))),
                    'to avoid breaking independence of plates "{}"'.format('", "'.join(diff)),
                ]), RuntimeWarning)

            irange_counters[name] = irange_counter
            if name in enumerated_sites:
                enumerated_contexts[context].add(name)


def check_if_enumerated(guide_trace):
    enumerated_sites = [name for name, site in guide_trace.nodes.items()
                        if site["type"] == "sample" and site["infer"].get("enumerate")]
    if enumerated_sites:
        warnings.warn('\n'.join([
            'Found sample sites configured for enumeration:'
            ', '.join(enumerated_sites),
            'If you want to enumerate sites, you need to use TraceEnum_ELBO instead.']))


@contextmanager
def optional(context_manager, condition):
    """
    Optionally wrap inside `context_manager` if condition is `True`.
    """
    if condition:
        with context_manager:
            yield
    else:
        yield


def deep_getattr(obj, name):
    """
    Python getattr() for arbitrarily deep attributes
    Throws an AttributeError if bad attribute
    """
    return functools.reduce(getattr, name.split("."), obj)
