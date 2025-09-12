from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailModelBackend(ModelBackend):
    """
    Custom backend to authenticate users using their email address.
    """

    def authenticate(self, request, email=None, password=None, **kwargs):
        UserModel = get_user_model()
        if email is None or password is None:
            return None

        try:
            user = UserModel.objects.get(email__iexact=email)
        except UserModel.DoesNotExist:
            UserModel().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None



