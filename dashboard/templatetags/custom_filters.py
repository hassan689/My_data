from django import template

register = template.Library()

@register.filter(name='attr')
def add_attr(field, css):
    """
    Adds HTML attributes to a Django form field.
    Usage: {{ field|attr:"id:my_id,class:my_class" }}
    """
    attrs = {}
    definition = css.split(',')

    for d in definition:
        if ':' not in d:
            continue
        key, value = d.split(':')
        attrs[key] = value

    return field.as_widget(attrs=attrs)