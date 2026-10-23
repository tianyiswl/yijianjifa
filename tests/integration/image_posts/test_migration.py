from sqlalchemy import inspect, text

from yijianjifa.storage.db import create_engine_for, migrate_to_revision


def test_migration_preserves_existing_video_record_and_schema(tmp_path):
    engine = create_engine_for(tmp_path / "migration.db")
    migrate_to_revision(engine, "0016_publish_deliveries")
    before_columns = inspect(engine).get_columns("finished_videos")
    with engine.begin() as connection:
        connection.execute(text("""INSERT INTO finished_videos
            (id,origin_type,display_name,content_hash,duration_ms,title,body,tags,task_locked,created_at,updated_at)
            VALUES ('existing-video','external','已有视频','video-hash',3000,'原标题','原正文','#原标签',0,'2026-10-14','2026-10-14')"""))
    migrate_to_revision(engine, "0017_image_posts")
    after_columns = inspect(engine).get_columns("finished_videos")
    assert [column["name"] for column in after_columns] == [column["name"] for column in before_columns]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT display_name,title,body,tags FROM finished_videos WHERE id='existing-video'" )).one() == ("已有视频", "原标题", "原正文", "#原标签")
    assert {"image_posts", "image_post_images"} <= set(inspect(engine).get_table_names())
