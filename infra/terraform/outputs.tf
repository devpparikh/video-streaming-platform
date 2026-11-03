output "videos_bucket_name" {
  description = "Name of the S3 bucket for uploads and DASH output."
  value       = aws_s3_bucket.videos.id
}

output "video_metadata_table" {
  description = "DynamoDB table name for video metadata."
  value       = aws_dynamodb_table.video_metadata.name
}
