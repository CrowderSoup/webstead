from django import forms


class CommentForm(forms.Form):
    author_name = forms.CharField(max_length=255, label="Name")
    author_email = forms.EmailField(required=False, label="Email")
    author_url = forms.URLField(required=False, label="Website")
    content = forms.CharField(widget=forms.Textarea, label="Comment")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "comment-field")
        self.fields["content"].widget.attrs.setdefault("rows", 4)
