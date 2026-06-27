from django.urls import path

from engram.access.auth_views import LoginView, LogoutView, MeView

urlpatterns = [
    path('login', LoginView.as_view(), name='auth-login'),
    path('me', MeView.as_view(), name='auth-me'),
    path('logout', LogoutView.as_view(), name='auth-logout'),
]
