from django.db.models.signals import post_delete, post_migrate, post_save
from django.dispatch import receiver

from api_app.visualizers_manager.apps import VisualizersManagerConfig
from api_app.visualizers_manager.models import VisualizerConfig


@receiver(post_save, sender=VisualizerConfig)
def post_save_visualizer_config(sender, instance: VisualizerConfig, *args, **kwargs):
    # delete list view cache
    instance.delete_class_cache_keys()


@receiver(post_delete, sender=VisualizerConfig)
def post_delete_visualizer_config(
    sender, instance: VisualizerConfig, using, origin, *args, **kwargs
):
    # delete list view cache
    instance.delete_class_cache_keys()


@receiver(post_migrate, sender=VisualizersManagerConfig)
def post_migrate_visualizer(
    sender, app_config, verbosity, interactive, stdout, using, plan, apps, **kwargs
):
    if plan:
        VisualizerConfig.delete_class_cache_keys()
