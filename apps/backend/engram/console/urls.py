from rest_framework.routers import DefaultRouter

from engram.console.views.organizations import OrganizationViewSet

router = DefaultRouter()

router.register('organizations', OrganizationViewSet, basename='admin-organization')

urlpatterns = router.urls
